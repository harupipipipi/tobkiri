"""Saved-turn strategy selection stays generic and fail closed."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import time

import pytest

from core_runtime.authority.v4 import AuthorityDenied
from core_runtime.bootstrap.saved_bridge import (
    STRATEGY_CATALOG,
    SavedBridgeCallbacks,
    _strategy_deadline,
    _strategy_idempotency_key,
    _strategy_maximum_cost,
)
from tobkiri_protocol.canonical import canonical_json
from tests.test_saved_bridge_callbacks import _setup as _bridge_setup
from tobkiri_protocol.saved_conversation import validate_saved_conversation_input


_REFERENCE = "third_party_strategy_pack.review.execute"


def _strategy_setup(tmp_path: Path, *, available: bool):
    store, outer, calls, callbacks = _bridge_setup(tmp_path)
    outer.payload["request"]["strategy_reference"] = _REFERENCE
    outer.deadline_monotonic = time.monotonic() + 60.0
    base_dispatch = callbacks._dispatch

    def dispatch(request, target, payload):
        if target == STRATEGY_CATALOG:
            calls.append((target, deepcopy(payload)))
            strategies = (
                [{"strategy_reference": _REFERENCE}] if available else []
            )
            return {
                "status": "ok",
                "value": {"strategies": strategies, "count": len(strategies)},
            }
        return base_dispatch(request, target, payload)

    return store, outer, calls, SavedBridgeCallbacks(
        dispatch,
        lambda request, targets: None,
    )


def test_saved_input_accepts_generic_strategy_reference() -> None:
    """Saved state names an installed provider, never a product feature flag."""

    request = {
        "turn_id": "turn-1",
        "conversation_id": "conversation-1",
        "conversation_revision": 1,
        "content": "Hello",
        "strategy_reference": _REFERENCE,
    }
    assert validate_saved_conversation_input({"request": request})["request"] == request
    with pytest.raises(ValueError, match="strategy reference"):
        validate_saved_conversation_input(
            {"request": {**request, "strategy_reference": "contains spaces"}}
        )
    limited = {**request, "strategy_maximum_cost_microusd": 50_000}
    assert validate_saved_conversation_input({"request": limited})["request"] == limited
    assert _strategy_maximum_cost(limited) == 50_000
    assert _strategy_maximum_cost(request) == 1_000_000
    with pytest.raises(ValueError, match="maximum cost"):
        validate_saved_conversation_input(
            {"request": {**request, "strategy_maximum_cost_microusd": 1_000_001}}
        )


def test_unavailable_selected_strategy_fails_before_user_append(
    tmp_path: Path,
) -> None:
    """A removed or disabled strategy cannot fall back or persist the user turn."""

    store, outer, calls, callbacks = _strategy_setup(tmp_path, available=False)
    before = store.path.read_bytes()

    with pytest.raises(AuthorityDenied, match="strategy is unavailable"):
        callbacks.preflight(outer)

    assert store.path.read_bytes() == before
    assert calls[-1] == (STRATEGY_CATALOG, {})


def test_available_selected_strategy_preflight_is_read_only(tmp_path: Path) -> None:
    """Availability is established only from the captured strategy catalog."""

    store, outer, calls, callbacks = _strategy_setup(tmp_path, available=True)
    before = store.path.read_bytes()

    callbacks.preflight(outer)

    assert store.path.read_bytes() == before
    assert calls[-1] == (STRATEGY_CATALOG, {})


def test_strategy_replay_key_is_stable_and_tool_steps_are_distinct() -> None:
    """A replay reuses one charge key while a later tool-loop step gets another."""

    request = {"turn_id": "turn-1"}
    conversation = {
        "conversation_revision": 2,
        "current_node_id": "message:user-1",
    }
    first = _strategy_idempotency_key(
        request, conversation, [], request_id="request-1"
    )
    replay = _strategy_idempotency_key(
        request, conversation, [], request_id="request-1"
    )
    after_tool = _strategy_idempotency_key(
        request,
        conversation,
        [{"role": "tool", "content": "result"}],
        request_id="request-1",
    )

    assert replay == first
    assert after_tool != first


def test_strategy_deadline_is_stable_and_bounded_by_outer_request() -> None:
    """Retries cannot renew the strategy's Host-owned execution deadline."""

    outer = type("Outer", (), {})()
    outer.deadline_monotonic = time.monotonic() + 30.0
    first = _strategy_deadline(outer)
    replay = _strategy_deadline(outer)

    assert replay == first
    assert time.time() * 1000 < first <= (time.time() + 31.0) * 1000
    assert canonical_json({"deadline": first})
