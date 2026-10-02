"""Real saved producer, Host result binding and owner completion regressions."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from core_runtime.bootstrap.saved_bridge import project_saved_ai_result
from ecosystem.defaultspack.runtime import saved_conversation as saved
from ecosystem.rumi_conversation_store_pack.runtime.store import ConversationConflict
from tests.test_saved_host_exchange import _Exchange
from tests.test_conversation_lifecycle_pack import _confirm
from tobkiri_protocol.conversation_lifecycle import active_task_gap_context

BASE_MS = 1_790_899_200_000


def _exchange(tmp_path: Path, ai: dict[str, Any]) -> _Exchange:
    """Use real producer/Host/owner with explicit finite public dispatch adapters."""
    exchange = _Exchange(tmp_path)
    original = exchange.callback._dispatch

    def dispatch(outer: Any, target: Any, payload: Any) -> Any:
        if target == saved.TARGETS[1]:
            exchange.calls.append((target, deepcopy(payload)))
            value = exchange.store.append_message(
                payload["conversation_id"],
                payload["message"],
                expected_conversation_revision=payload["expected_conversation_revision"],
                saved_input=outer.payload,
            )
            return {"status": "ok", "value": value}
        if target == saved.TARGETS[2]:
            exchange.calls.append((target, deepcopy(payload)))
            return {"status": "ok", "value": project_saved_ai_result(ai)}
        return original(outer, target, payload)

    exchange.callback._dispatch = dispatch
    return exchange


@pytest.mark.parametrize(
    "signal,state",
    [
        ({"finish_reason": "stop"}, "running"),
        ({}, "unknown"),
        ({"finish_reason": "waiting_user"}, "waiting_user"),
        ({"finish_reason": "stop", "task_state": "waiting_user"}, "waiting_user"),
        ({"finish_reason": "stop", "task_state": "waiting_approval"}, "waiting_approval"),
        ({"finish_reason": "stop", "task_state": "failed"}, "failed"),
        ({"finish_reason": "stop", "task_state": "idle"}, "unknown"),
        ({"finish_reason": "stop", "task_state": "invalid"}, "unknown"),
        ({"finish_reason": "stop", "metadata": {"thinking": {"state": "failed"}}}, "failed"),
        ({"finish_reason": "cancelled"}, "cancelled"),
        ({"finish_reason": "tool_calls"}, "running"),
    ],
)
def test_real_producer_stamps_only_authenticated_successful_terminal_result(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signal: dict[str, Any],
    state: str,
) -> None:
    monkeypatch.setattr(
        "ecosystem.rumi_conversation_store_pack.runtime.store._now_ms",
        lambda: BASE_MS,
    )
    exchange = _exchange(
        tmp_path,
        {
            "status": "ok",
            "output": "Which option should I use?",
            **signal,
        },
    )
    for _ in range(4):
        exchange.step()
    exchange.host.finish(exchange.intent)
    source = exchange.store.get("conversation-1")["lifecycle"]
    assert source["state"] == state
    assert source["completed_at_ms"] is None
    if signal == {"finish_reason": "stop"}:
        assert source["completion_candidate"]["completed_at_ms"] == BASE_MS
        _confirm(exchange.store, exchange.outer.payload)
        confirmed = exchange.store.get("conversation-1")["lifecycle"]
        assert confirmed["state"] == "completed"
        assert confirmed["completed_at_ms"] == BASE_MS


def test_guest_cannot_upgrade_authenticated_wait_to_completed(
    tmp_path: Path,
) -> None:
    exchange = _exchange(
        tmp_path,
        {
            "status": "ok",
            "output": "Approve the next step?",
            "finish_reason": "stop",
            "task_state": "waiting_approval",
        },
    )
    for _ in range(3):
        exchange.step()
    exchange.intent["payload"]["message"]["finish_reason"] = "stop"
    before = exchange.store.path.read_bytes()
    with pytest.raises(ValueError, match="assistant differs"):
        exchange.step()
    assert exchange.store.path.read_bytes() == before


@pytest.mark.parametrize("elapsed,has_gap", [(3_599_000, False), (3_600_000, True)])
def test_saved_ai_reads_atomic_receipt_gap_without_rewriting_user_body(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    elapsed: int,
    has_gap: bool,
) -> None:
    now = [BASE_MS]
    monkeypatch.setattr(
        "ecosystem.rumi_conversation_store_pack.runtime.store._now_ms",
        lambda: now[0],
    )
    exchange = _exchange(
        tmp_path,
        {
            "status": "ok",
            "output": "Done",
            "finish_reason": "stop",
        },
    )
    for _ in range(4):
        exchange.step()
    _confirm(exchange.store, exchange.outer.payload)
    now[0] += elapsed
    exchange.outer.payload["request"] = {
        **exchange.outer.payload["request"],
        "turn_id": "turn-2",
        "conversation_revision": exchange.store.get("conversation-1")["conversation_revision"],
        "content": "どう？",
    }
    # A new authenticated exchange owns its own continuation identity/state.
    from tests.test_saved_bridge_callbacks import _frame

    intent = saved.start(exchange.outer.payload["request"])
    for _ in range(3):
        intent = saved.resume(
            intent["state"],
            exchange.callback(exchange.outer, _frame(intent)),
        )
    messages = [
        payload["messages"] for target, payload in exchange.calls if target == saved.TARGETS[2]
    ][-1]
    timing = [message for message in messages if message["role"] == "system"]
    assert bool(timing) is has_gap
    if has_gap:
        assert "elapsed_seconds: 3600" in timing[0]["content"]
    assert messages[-1] == {"role": "user", "content": "どう？"}
    assert (active_task_gap_context(exchange.store.get("conversation-1")) is not None) is has_gap


def test_stale_parallel_terminal_cannot_complete_resumed_turn(tmp_path: Path) -> None:
    exchange = _exchange(
        tmp_path,
        {
            "status": "ok",
            "output": "Done",
            "finish_reason": "stop",
        },
    )
    for _ in range(3):
        exchange.step()
    exchange.store.append_message(
        "conversation-1",
        {"id": "new-user", "role": "user", "content": "Continue"},
        expected_conversation_revision=2,
    )
    with pytest.raises(ConversationConflict):
        exchange.step()
    source = exchange.store.get("conversation-1")["lifecycle"]
    assert source["state"] == "running"
    assert source["completed_at_ms"] is None


def test_ordinary_append_and_update_cannot_forge_owner_completion(
    tmp_path: Path,
) -> None:
    exchange = _Exchange(tmp_path)
    exchange.store.append_message(
        "conversation-1",
        {"id": "user", "role": "user"},
        expected_conversation_revision=1,
    )
    exchange.store.append_message(
        "conversation-1",
        {
            "id": "fake",
            "role": "assistant",
            "parent_id": "user",
            "status": "complete",
            "finish_reason": "stop",
        },
        expected_conversation_revision=2,
    )
    exchange.store.mutate_message(
        "conversation-1",
        "fake",
        patch={"finish_reason": "completed"},
        expected_conversation_revision=3,
    )
    assert exchange.store.get("conversation-1")["lifecycle"]["completed_at_ms"] is None


@pytest.mark.parametrize(
    "evidence,state",
    [
        ({"task_state": "waiting_user"}, "waiting_user"),
        ({"metadata": {"approval_required": True}}, "waiting_approval"),
        ({"metadata": {"thinking": {"state": "failed"}}}, "failed"),
        ({"metadata": {"cancelled": True}}, "cancelled"),
        ({"task_state": "idle"}, "unknown"),
    ],
)
def test_gateway_retains_explicit_provider_terminal_evidence(
    evidence: dict[str, Any],
    state: str,
) -> None:
    from types import SimpleNamespace
    from ecosystem.rumi_ai_gateway_pack.runtime.gateway import _normalize_result

    selected = SimpleNamespace(
        model_id="model",
        provider_instance_id="provider",
        catalog_provider_instance_id="catalog",
        catalog_revision="revision",
    )
    result = _normalize_result(
        {"status": "ok", "output": "Which option?", "finish_reason": "stop", **evidence},
        "request",
        selected,
    )
    assert result["task_state"] == state
    assert "metadata" not in result
    assert project_saved_ai_result(result)["finish_reason"] == {
        "failed": "error",
        "unknown": None,
    }.get(state, state)
