"""History admission through real owner writes and the Host/Guest saved ABI.

AI, route readiness and optional live-progress delivery are finite adapters;
these tests do not claim a model, guest sandbox or native-budget qualification.
"""

from __future__ import annotations

import base64
import hashlib
from copy import deepcopy
from pathlib import Path
import time

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.saved_bridge import (
    READINESS, STRATEGY, STRATEGY_CATALOG, SavedBridgeCallbacks,
)
from ecosystem.rumi_ai_gateway_pack.runtime.route_quote import _validate_quote_input
from ecosystem.rumi_prompt_studio_pack.runtime.store import PromptStudioStore
from ecosystem.tobkiri_conversation_orchestration_pack.runtime import saved_conversation as guest
from tests.test_saved_bridge_callbacks import _setup
from tobkiri_host.continuation_chain import ChainIdentity, ContinuationChains
from tobkiri_host.continuation_envelope import seal_continuation_intent
from tobkiri_host.saved_host_exchange import SavedHostExchange
from tobkiri_protocol.canonical import canonical_digest, canonical_json, strict_loads
from tobkiri_protocol.saved_context import saved_prompt_digest
from tobkiri_protocol.saved_conversation import (
    MAX_SAVED_INPUT_BYTES, MAX_SAVED_CHAIN_BYTES, MAX_SAVED_FRAME_BYTES,
    MAX_SAVED_TURN_LIFETIME_SECONDS,
)
from tobkiri_protocol.saved_messages import build_saved_model_messages
from tobkiri_protocol.saved_tools import MAX_SAVED_TOOL_HOPS, saved_tool_logs
from tobkiri_protocol.turn_progress_v1 import AI_STREAM, ACTION, ACTION_OPERATION


_READ_BYTES = 512 * 1024
_STRATEGY = "third_party_strategy_pack.review.execute"


def _message(identifier, *, parent=None, content="Earlier", role="assistant", **extra):
    return {"id": identifier, "parent_id": parent, "role": role,
            "content": content, "status": "complete", **extra}


def _linear(count):
    return [_message(f"history-{index}", parent=f"history-{index - 1}" if index else None,
                     role="user" if index % 2 == 0 else "assistant")
            for index in range(count)]


def _read_value(conversation, *, delivery=None, prompt=None):
    """The public Guest read ABI, independently encoded by the actual Guest."""
    value = {"conversation": {
        key: conversation.get(key)
        for key in ("id", "conversation_revision", "model_reference", "current_node_id",
                    "agent_id", "system_prompt_id")
    }}
    value["conversation"]["messages"] = [{"id": item["id"]} for item in conversation["messages"]]
    if delivery is not None:
        value["delivery_mode"] = delivery
    if prompt is not None:
        value["system_prompt_digest"] = saved_prompt_digest(prompt)
    return value


class _HistoryExchange:
    def __init__(self, root: Path, *, mode="buffered"):
        self.store, self.outer, self.calls, base = _setup(root)
        self.mode = mode
        self.outer.deadline_monotonic = time.monotonic() + 60
        self.generated = []
        self.read_values = []

        def dispatch(outer, target, payload):
            if target == READINESS:
                _validate_quote_input(payload)
            elif target == STRATEGY_CATALOG:
                self.calls.append((target, deepcopy(payload)))
                return {"status": "ok", "value": {
                    "strategies": [{"strategy_reference": _STRATEGY}], "count": 1,
                }}
            elif target in (guest.TARGETS[2], AI_STREAM, STRATEGY):
                self.calls.append((target, deepcopy(payload)))
                self.generated.append(deepcopy(payload["request"] if target == STRATEGY else payload))
                return {"status": "ok", "value": {"status": "ok", "output": "Hi"}}
            elif target == (ACTION, ACTION_OPERATION):
                self.calls.append((target, deepcopy(payload)))
                return {"status": "ok", "value": {"progress_id": "progress-1"}}
            return base._dispatch(outer, target, payload)

        self.callbacks = SavedBridgeCallbacks(
            dispatch, lambda outer, targets: None,
            (lambda outer, request, conversation: True)
            if mode in ("incremental", "strategy") else None,
        )
        if mode == "strategy":
            self.outer.payload["request"]["strategy_reference"] = _STRATEGY

    def seed(self, messages):
        before = self.store.get("conversation-1")
        self.store.replace_messages("conversation-1", messages,
                                    expected_conversation_revision=before["conversation_revision"])
        self.outer.payload["request"]["conversation_revision"] = self.store.get("conversation-1")["conversation_revision"]

    def start(self, *, next_turn=False):
        request = self.outer.payload["request"]
        if next_turn:
            request["turn_id"] += "-next"
            request["conversation_revision"] = self.store.get("conversation-1")["conversation_revision"]
            self.outer.context.request_id += "-next"
        self.identity = ChainIdentity("domain", self.outer.context.request_id, "sha256:" + "a" * 64, 60)
        self.host = SavedHostExchange(
            self.identity, request_digest="sha256:" + "c" * 64,
            artifact_identity="artifact", deadline_text="60",
            chains=ContinuationChains(
                clock=lambda: 0, max_bytes=MAX_SAVED_CHAIN_BYTES,
                max_hops=MAX_SAVED_TOOL_HOPS, max_lifetime=MAX_SAVED_TURN_LIFETIME_SECONDS,
            ), request=request,
        )
        self.previous = None
        self.intent = guest.start(request)

    def step(self, *, lost_ack=False):
        intent = self.intent
        target = (intent["target"]["contract_id"], intent["target"]["operation_id"])
        sealed = seal_continuation_intent(
            canonical_json(intent), identity=self.identity, hop=intent["hop"],
            previous_digest=self.previous, target=target, nonce=str(intent["hop"]) * 48,
            max_intent_bytes=MAX_SAVED_INPUT_BYTES, max_frame_bytes=MAX_SAVED_FRAME_BYTES,
            max_hops=MAX_SAVED_TOOL_HOPS,
        )
        wrapper = {
            "kind": "tobkiri.packvm.bridge.host-request.v2",
            "protocol": "io.tobkiri.packvm.bridge.v2", "version": 2,
            "request_id": self.identity.request_id, "target_domain": "domain",
            "binding_digest": self.identity.binding_digest,
            "guest_artifact_identity": "artifact", "request_digest": "sha256:" + "c" * 64,
            "deadline_monotonic": "60", "bridge_request": strict_loads(sealed.frame),
            "bridge_request_digest": sealed.digest,
        }
        checked = self.host.accept(wrapper)
        self.last_wrapper = wrapper
        outcome = self.callbacks(self.outer, self.host.callback_frame(checked))
        if intent["state"]["stage"] == "read":
            guest._json(outcome["value"], limit=_READ_BYTES)
            self.read_values.append(deepcopy(outcome["value"]))
        if lost_ack:
            outcome = {"status": "error", "error": {"code": "TIMEOUT", "message": "unknown"}}
        result = self.host.result(outcome)
        self.previous = canonical_digest(result["bridge_result"])
        self.intent = guest.resume(intent["state"], outcome)
        return outcome

    def complete(self):
        self.callbacks.preflight(self.outer)
        self.start()
        for _ in range(4):
            self.step()
        self.host.finish(self.intent)
        assert self.intent["status"] == "ok"


@pytest.mark.parametrize("count", [198, 199, 200, 201])
@pytest.mark.parametrize("mode", ["buffered", "incremental", "strategy"])
def test_history_count_is_not_conversation_lifetime(tmp_path, count, mode):
    exchange = _HistoryExchange(tmp_path, mode=mode)
    exchange.seed(_linear(count))
    before = exchange.store.get("conversation-1")
    original_content = [(item["id"], item["role"], item["content"], item["parent_id"])
                        for item in before["messages"]]
    expected = build_saved_model_messages(before)
    exchange.complete()
    after = exchange.store.get("conversation-1")
    assert len(after["messages"]) == count + 2
    assert [(item["id"], item["role"], item["content"], item["parent_id"])
            for item in after["messages"][:-2]] == original_content
    assert after["id"] == before["id"] == "conversation-1"
    assert after["conversation_revision"] == before["conversation_revision"] + 2
    assert exchange.generated[0]["messages"] == [*expected, {"role": "user", "content": "Hello"}]
    assert len(exchange.generated) == 1
    assert after["messages"][-2]["id"] == guest._message_id(exchange.outer.payload["request"], "user")
    assert after["messages"][-1]["id"] == guest._message_id(exchange.outer.payload["request"], "assistant")
    exchange.start(next_turn=True)
    exchange.callbacks.preflight(exchange.outer)
    for _ in range(4):
        exchange.step()
    exchange.host.finish(exchange.intent)
    assert exchange.intent["status"] == "ok"
    assert len(exchange.generated) == 2
    assert len(exchange.store.get("conversation-1")["messages"]) == count + 4


def test_large_unselected_branch_is_kept_out_of_model_but_not_deleted(tmp_path):
    exchange = _HistoryExchange(tmp_path)
    exchange.seed([_message("root", role="system", content="Owned system instruction"),
                   *[_message(f"other-{index}", parent="root", content="Unselected") for index in range(600)],
                   _message("chosen", parent="root", content="Selected")])
    before = exchange.store.get("conversation-1")
    exchange.complete()
    after = exchange.store.get("conversation-1")
    assert len(after["messages"]) == len(before["messages"]) + 2
    assert [item["id"] for item in after["messages"][:-2]] == [item["id"] for item in before["messages"]]
    assert exchange.generated[0]["messages"] == [
        {"role": "system", "content": "Owned system instruction"},
        {"role": "assistant", "content": "Selected"},
        {"role": "user", "content": "Hello"},
    ]
    exchange.start(next_turn=True)
    exchange.callbacks.preflight(exchange.outer)
    for _ in range(4):
        exchange.step()
    exchange.host.finish(exchange.intent)
    assert len(exchange.store.get("conversation-1")["messages"]) == len(before["messages"]) + 4
    assert len(exchange.generated) == 2


@pytest.mark.parametrize("role", ["user", "assistant"])
@pytest.mark.parametrize("mode", ["buffered", "incremental", "strategy"])
def test_turn_id_on_unselected_branch_reconciles_without_model_or_append(tmp_path, role, mode):
    exchange = _HistoryExchange(tmp_path, mode=mode)
    request = exchange.outer.payload["request"]
    exchange.seed([_message("root"), *[_message(f"other-{index}", parent="root") for index in range(201)],
                   _message(guest._message_id(request, role), parent="root", role=role),
                   _message("chosen", parent="root")])
    before = exchange.store.path.read_bytes()
    exchange.callbacks.preflight(exchange.outer)
    assert all(target != READINESS for target, _ in exchange.calls)
    exchange.start()
    exchange.step()
    assert exchange.intent["error"]["code"] == "TURN_RECONCILIATION_REQUIRED"
    exchange.host.finish(exchange.intent)
    assert not exchange.generated
    assert all(target not in (READINESS, STRATEGY_CATALOG) for target, _ in exchange.calls)
    assert exchange.store.path.read_bytes() == before


@pytest.mark.parametrize("stage", ["user", "assistant"])
def test_lost_append_ack_and_replay_never_duplicate_history(tmp_path, stage):
    exchange = _HistoryExchange(tmp_path)
    exchange.seed(_linear(201))
    exchange.callbacks.preflight(exchange.outer)
    exchange.start()
    exchange.step()
    if stage == "assistant":
        exchange.step()
        exchange.step()
    exchange.step(lost_ack=True)
    assert exchange.intent["reconciliation_required"] is True
    exchange.host.finish(exchange.intent)
    before = exchange.store.path.read_bytes()
    ai_count = len(exchange.generated)
    exchange.outer.payload["request"]["conversation_revision"] = exchange.store.get("conversation-1")["conversation_revision"]
    exchange.outer.context.request_id += "-replay"
    exchange.callbacks.preflight(exchange.outer)
    exchange.start()
    exchange.step()
    assert exchange.intent["error"]["code"] == "TURN_RECONCILIATION_REQUIRED"
    exchange.host.finish(exchange.intent)
    assert exchange.store.path.read_bytes() == before
    assert len(exchange.generated) == ai_count


def test_replayed_user_frame_cannot_append_again_and_valid_resume_still_completes(tmp_path):
    exchange = _HistoryExchange(tmp_path)
    exchange.seed(_linear(201))
    exchange.callbacks.preflight(exchange.outer)
    exchange.start()
    exchange.step()
    exchange.step()
    before = exchange.store.path.read_bytes()
    with pytest.raises(ValueError):
        exchange.host.accept(deepcopy(exchange.last_wrapper))
    assert exchange.store.path.read_bytes() == before
    assert not exchange.generated
    exchange.step()
    exchange.step()
    exchange.host.finish(exchange.intent)
    assert exchange.intent["status"] == "ok"
    assert len(exchange.generated) == 1
    assert len(exchange.store.get("conversation-1")["messages"]) == 203


def _fit_read_history(exchange, *, remaining, prompt=None):
    """Fit actual Guest encoding using owner-valid IDs, including UTF-8 fields."""
    before = exchange.store.get("conversation-1")
    messages = [_message("root"), _message("pad-a", parent="root"),
                _message("pad-b", parent="root"), _message("chosen", parent="root")]
    record = {**before, "messages": messages, "current_node_id": "chosen",
              "conversation_revision": before["conversation_revision"] + 1}
    delivery = ("incremental" if exchange.mode == "incremental" else
                "buffered" if exchange.mode == "strategy" else None)
    value = _read_value(record, delivery=delivery, prompt=prompt)
    target = _READ_BYTES - remaining
    # An ASCII ID of 256 bytes contributes exactly 266 encoded bytes including
    # its object and comma. Two independent padding IDs fill the remainder.
    count = (target - len(guest._json(value))) // 266
    messages[1:1] = [_message(f"m{index:06d}." + "x" * 248, parent="root") for index in range(count)]
    value = _read_value(record, delivery=delivery, prompt=prompt)
    gap = target - len(guest._json(value))
    messages[-3]["id"] += "x" * min(gap, 240)
    messages[-2]["id"] += "x" * max(0, gap - 240)
    exchange.seed(messages)
    actual = _read_value(exchange.store.get("conversation-1"), delivery=delivery, prompt=prompt)
    assert len(guest._json(actual, limit=_READ_BYTES)) == target
    return actual


def test_near_read_byte_limit_keeps_every_id_through_two_complete_turns(tmp_path):
    exchange = _HistoryExchange(tmp_path)
    original = _fit_read_history(exchange, remaining=512)
    count = len(original["conversation"]["messages"])
    assert count > 1900
    exchange.complete()
    assert exchange.read_values[0] == original
    exchange.start(next_turn=True)
    exchange.callbacks.preflight(exchange.outer)
    for _ in range(4):
        exchange.step()
    exchange.host.finish(exchange.intent)
    assert exchange.intent["status"] == "ok"
    assert len(exchange.store.get("conversation-1")["messages"]) == count + 4
    assert len(exchange.read_values[1]["conversation"]["messages"]) == count + 2
    assert len(exchange.generated) == 2
    assert len(exchange.generated[0]["messages"]) == 3


@pytest.mark.parametrize("role", ["user", "assistant"])
def test_existing_turn_at_read_boundary_does_not_count_new_append_slots(tmp_path, role):
    exchange = _HistoryExchange(tmp_path)
    _fit_read_history(exchange, remaining=0)
    messages = exchange.store.get("conversation-1")["messages"]
    messages[1]["id"] = guest._message_id(exchange.outer.payload["request"], role)
    messages[1]["role"] = role
    exchange.seed(messages)
    value = _read_value(exchange.store.get("conversation-1"))
    assert 0 <= _READ_BYTES - len(guest._json(value, limit=_READ_BYTES)) < 200
    before = exchange.store.path.read_bytes()
    exchange.callbacks.preflight(exchange.outer)
    exchange.start()
    exchange.step()
    assert exchange.intent["error"]["code"] == "TURN_RECONCILIATION_REQUIRED"
    exchange.host.finish(exchange.intent)
    assert exchange.store.path.read_bytes() == before
    assert not exchange.generated


@pytest.mark.parametrize("count", [2047, 2048])
def test_actual_quote_message_bound_includes_pending_user(tmp_path, count):
    exchange = _HistoryExchange(tmp_path)
    exchange.seed(_linear(count))
    if count == 2047:
        exchange.complete()
        assert len(exchange.generated[0]["messages"]) == 2048
        exchange.start(next_turn=True)
    before = exchange.store.path.read_bytes()
    with pytest.raises(ValueError, match="AI route quote input is invalid"):
        exchange.callbacks.preflight(exchange.outer)
    assert exchange.store.path.read_bytes() == before
    assert len(exchange.generated) == (1 if count == 2047 else 0)


@pytest.mark.parametrize("remaining", [100, 200])
@pytest.mark.parametrize("stage", ["preflight", "user"])
def test_planned_read_encoding_overflow_stops_before_user_append(tmp_path, remaining, stage):
    exchange = _HistoryExchange(tmp_path, mode="incremental")
    # The current revision is nine, so the user's actual owner revision also
    # adds a decimal digit. Include prompt digest and delivery-mode fields.
    prompt_store = PromptStudioStore("defaults", user_data_root=tmp_path)
    prompt_store.save("system", "Instructions", expected_body_hash="sha256:" + hashlib.sha256(b"").hexdigest())
    exchange.store.update("conversation-1", {"system_prompt_id": "system", "model_reference": "model:日本語"},
                          expected_conversation_revision=1)
    for _ in range(6):
        record = exchange.store.get("conversation-1")
        exchange.store.update("conversation-1", {"title": "容量"}, expected_conversation_revision=record["conversation_revision"])
    prompt = prompt_store.get("system")
    actual = _fit_read_history(exchange, remaining=remaining, prompt=prompt)
    future = deepcopy(actual)
    request = exchange.outer.payload["request"]
    first_overflow = None
    for role in ("user", "assistant"):
        projected = future["conversation"]
        projected["conversation_revision"] += 1
        projected["current_node_id"] = guest._message_id(request, role)
        projected["messages"].append({"id": guest._message_id(request, role)})
        try:
            guest._json(future, limit=_READ_BYTES)
        except ValueError:
            first_overflow = role
            break
    assert first_overflow == ("user" if remaining == 100 else "assistant")
    before = exchange.store.path.read_bytes()
    if stage == "preflight":
        with pytest.raises(AuthorityDenied, match="read exceeds continuation budget"):
            exchange.callbacks.preflight(exchange.outer)
    else:
        # The real read callback has the same admission check even if a caller
        # omitted preflight; it must not expose an appendable Guest intent.
        exchange.start()
        with pytest.raises(AuthorityDenied, match="read exceeds continuation budget"):
            exchange.step()
    assert exchange.store.path.read_bytes() == before
    assert not exchange.generated
    assert all(target != READINESS for target, _ in exchange.calls)


@pytest.mark.parametrize("mode", ["buffered", "strategy"])
def test_full_id_projection_over_read_limit_is_explicit_and_read_only(tmp_path, mode):
    exchange = _HistoryExchange(tmp_path, mode=mode)
    _fit_read_history(exchange, remaining=0)
    record = exchange.store.get("conversation-1")
    exchange.store.append_message("conversation-1", _message("one-more", parent="chosen"),
                                  expected_conversation_revision=record["conversation_revision"])
    exchange.outer.payload["request"]["conversation_revision"] = exchange.store.get("conversation-1")["conversation_revision"]
    before = exchange.store.path.read_bytes()
    with pytest.raises(AuthorityDenied, match="read exceeds continuation budget"):
        exchange.callbacks.preflight(exchange.outer)
    assert exchange.store.path.read_bytes() == before
    assert not exchange.generated
    assert all(target not in (READINESS, STRATEGY_CATALOG) for target, _ in exchange.calls)


@pytest.mark.parametrize("mode", ["buffered", "strategy"])
@pytest.mark.parametrize("stage", ["preflight", "user"])
def test_actual_pending_image_model_bytes_are_checked_before_saving(tmp_path, mode, stage):
    exchange = _HistoryExchange(tmp_path, mode=mode)
    messages = _linear(64)
    for item in messages:
        item["content"] = "x" * 35_000
    exchange.seed(messages)
    image = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * (1024 * 1024 - 8)).decode("ascii")
    exchange.outer.payload["request"]["content"] = [
        {"type": "text", "text": "Describe"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64," + image}},
    ]
    # Text-only readiness fits, while actual structured generation exceeds the
    # unchanged three-MiB bound. The image must not disappear to make it fit.
    record = exchange.store.get("conversation-1")
    assert len(canonical_json({"messages": build_saved_model_messages(
        record, pending_user_content=exchange.outer.payload["request"]["content"],
        flatten_text_blocks=True,
    )})) < MAX_SAVED_INPUT_BYTES
    before = exchange.store.path.read_bytes()
    if stage == "preflight":
        with pytest.raises(AuthorityDenied, match="AI input exceeds budget"):
            exchange.callbacks.preflight(exchange.outer)
    else:
        exchange.start()
        exchange.step()
        with pytest.raises(AuthorityDenied, match="AI input exceeds budget"):
            exchange.step()
    assert exchange.store.path.read_bytes() == before
    assert not exchange.generated
    assert all(target not in (READINESS, STRATEGY_CATALOG) for target, _ in exchange.calls)


@pytest.mark.parametrize("change", ["role", "status", "tool_logs"])
def test_selected_invalid_history_still_rejects_above_200(tmp_path, change):
    exchange = _HistoryExchange(tmp_path)
    messages = _linear(201)
    messages[-1][change] = {"role": "tool", "status": "streaming", "tool_logs": [{"unowned": True}]}[change]
    exchange.seed(messages)
    before = exchange.store.path.read_bytes()
    with pytest.raises(AuthorityDenied):
        exchange.callbacks.preflight(exchange.outer)
    assert exchange.store.path.read_bytes() == before
    assert not exchange.generated


def test_owned_tool_roles_survive_large_history_and_pending_user_is_once(tmp_path):
    exchange = _HistoryExchange(tmp_path)
    messages = _linear(201)
    trace = [
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-1", "type": "function", "function": {"name": "search", "arguments": "{}"}},
        ]},
        {"role": "tool", "tool_call_id": "call-1", "content": "Found"},
    ]
    messages[-1].update(role="assistant", metadata={"saved_tool_messages": trace}, tool_logs=saved_tool_logs(trace))
    exchange.seed(messages)
    exchange.complete()
    assert exchange.generated[0]["messages"][-4:] == [
        *trace, {"role": "assistant", "content": "Earlier"}, {"role": "user", "content": "Hello"},
    ]


@pytest.mark.parametrize("when", ["before_user", "after_user"])
def test_history_capacity_pass_never_bypasses_revision_conflict(tmp_path, when):
    exchange = _HistoryExchange(tmp_path)
    exchange.seed(_linear(201))
    exchange.callbacks.preflight(exchange.outer)
    exchange.start()
    exchange.step()
    if when == "after_user":
        exchange.step()
    record = exchange.store.get("conversation-1")
    exchange.store.append_message("conversation-1", _message("concurrent", parent=record["current_node_id"]),
                                  expected_conversation_revision=record["conversation_revision"])
    before = exchange.store.path.read_bytes()
    with pytest.raises(AuthorityDenied):
        exchange.step()
    assert exchange.store.path.read_bytes() == before
    assert not exchange.generated
